"""The owner's statements on staff members and login links (M8 PR 1; S5, D4 (a), migration 0008).

The app role may only read `staff_users` and mark a link used; creating a member, issuing a link
and disabling a member are the owner's (rule 5: staff rows live in the database only, written by
`python -m vectapilot_api.staff`). Each function takes a connection inside the business's
transaction (`db.business_transaction`, which sets `app.business_id`), and each statement names
the business itself: the owner is the bootstrap superuser locally and in CI (ADR 0009, "Limit"),
and RLS never applies to it, FORCE or not.

- **`add_member`**: a synthetic display name and a role (P2); the database draws the id.
- **`issue_link`**: a fresh token (`auth.tokens.make_token`), stored as its SHA-256 with
  `expires_at` from the database's clock, only for an enabled member of the business. The token
  is returned to the caller once and never stored or logged.
- **`disable_member`**: sets `disabled_at` once and, in the same transaction, revokes every live
  session of that member (zara's review). Takes effect on the member's next request (every staff
  lookup reads it); a link issued earlier is used up by a login that opens no session. A hand
  re-enable (there is no enable command) therefore brings back no session.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.auth.store import MAX_LINK_LIFETIME_S, StaffRole, check_lifetime
from vectapilot_api.auth.tokens import make_token, token_sha256
from vectapilot_api.gateway.types import BusinessId

ADD_MEMBER: Final = text(
    "INSERT INTO staff_users (business_id, display_name, role)"
    " VALUES (:business_id, :display_name, :role) RETURNING id"
)
ISSUE_LINK: Final = text(
    "INSERT INTO staff_login_links (business_id, staff_id, token_sha256, expires_at)"
    " SELECT business_id, id, :token_sha256,"
    " now() + make_interval(secs => CAST(:lifetime_s AS double precision))"
    " FROM staff_users"
    " WHERE business_id = :business_id AND id = :staff_id AND disabled_at IS NULL"
    " RETURNING expires_at"
)
"""Only for an enabled member of this business: no row inserted otherwise."""
DISABLE_MEMBER: Final = text(
    "UPDATE staff_users SET disabled_at = now()"
    " WHERE business_id = :business_id AND id = :staff_id AND disabled_at IS NULL"
    " RETURNING id"
)
REVOKE_MEMBER_SESSIONS: Final = text(
    "UPDATE sessions SET revoked_at = now()"
    " WHERE business_id = :business_id AND staff_id = :staff_id AND revoked_at IS NULL"
)
"""Every unrevoked session of the member just disabled (an expired one too: harmless, and the
owner's connection has no expiry policy)."""


@dataclass(frozen=True)
class IssuedLink:
    """A link just issued: its token (shown once) and its expiry."""

    token: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"IssuedLink(expires_at={self.expires_at!r})"  # never the token (S3)


async def add_member(
    connection: AsyncConnection, business_id: BusinessId, display_name: str, role: StaffRole
) -> UUID:
    """A new enabled member of `business_id`; its id."""
    staff_id: UUID = (
        await connection.execute(
            ADD_MEMBER,
            {"business_id": business_id, "display_name": display_name, "role": role.value},
        )
    ).scalar_one()
    return staff_id


async def issue_link(
    connection: AsyncConnection, business_id: BusinessId, staff_id: UUID, *, lifetime_s: int
) -> IssuedLink | None:
    """A one-time link for an enabled member of `business_id`, or None when there is none.
    `ValueError` before any SQL for a lifetime outside migration 0008's CHECK."""
    check_lifetime(lifetime_s, MAX_LINK_LIFETIME_S)
    token = make_token(business_id)
    row = (
        await connection.execute(
            ISSUE_LINK,
            {
                "business_id": business_id,
                "staff_id": staff_id,
                "token_sha256": token_sha256(token),
                "lifetime_s": lifetime_s,
            },
        )
    ).one_or_none()
    return None if row is None else IssuedLink(token=token, expires_at=row.expires_at)


async def disable_member(
    connection: AsyncConnection, business_id: BusinessId, staff_id: UUID
) -> bool:
    """Disable an enabled member of `business_id` and revoke their live sessions, in the caller's
    one transaction; whether one was disabled now (False: nothing is changed)."""
    params = {"business_id": business_id, "staff_id": staff_id}
    row = (await connection.execute(DISABLE_MEMBER, params)).one_or_none()
    if row is None:
        return False
    await connection.execute(REVOKE_MEMBER_SESSIONS, params)
    return True
