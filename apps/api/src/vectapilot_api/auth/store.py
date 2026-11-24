"""The statements on migration 0008's tables (M8 PR 1; S3, S4, S5, ADR 0019).

Every function takes an open connection inside the business's transaction
(`Database.business_transaction(business_id)`): RLS then shows only that business's rows. Every
statement also names the business itself (`business_id = :business_id`), so on a connection where
RLS cannot help (the owner's, a superuser) the statement alone still keeps another business out;
the canary suite runs the lookups and the link use there too (`tests/canary/read_paths.py`,
`OWNER_CONNECTION_PATHS`), and its red proofs remove each clause.

**What a lookup checks, all in its own SQL** (migration 0008, "Hidden at expiry"): the business,
the token's hash, the session's `kind` (so a customer token never opens a staff route and a staff
token never a widget route, S1), `expires_at > now()` and `revoked_at IS NULL`; a staff lookup
also joins the staff member, within the same business, and requires `disabled_at IS NULL`. The app
role's restrictive policy hides an expired row as well, but the clause stays: the owner's
connection has no policy. The hash read back is compared once more in constant time
(`tokens.same_hash`). A lookup returns at most one row (`UNIQUE (business_id, token_sha256)`);
more than one is a broken statement, never a choice, and raises (`MultipleResultsFound`, a
`SQLAlchemyError`: the dependency's typed 503, fail closed).

**Clocks.** `created_at`, `expires_at`, `revoked_at` and `used_at` all come from the database's
`now()`, never from the API's clock: one clock for every comparison (P1).

**S5's link use** is one statement: `UPDATE ... SET used_at = now() WHERE ... AND used_at IS NULL
AND expires_at > now() RETURNING`. Two parallel uses of one link: the second waits for the first's
row lock and then re-checks its WHERE against the updated row (READ COMMITTED), so exactly one
returns the staff member. The same statement reads whether that member is enabled (zara's review:
a disabled member's link is used up and opens no session), with a scalar subquery that pairs the
business: on a connection without RLS, a subquery that lost the pairing reads both businesses'
members of a shared id and raises (SQLSTATE 21000) instead of choosing one. Migration 0008's
`revoke_once` and `use_once` make revocation and use one-way for the app role.

Nothing here logs: a token, a hash or a cookie never reaches a log line (S3).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final
from uuid import UUID

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.auth.tokens import ParsedToken, same_hash
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.tools.config import CustomerRef

MAX_SESSION_LIFETIME_S: Final = 24 * 60 * 60
"""Migration 0008's CHECK: a session lives at most 24 hours (P1's 12 h and 8 h are settings)."""
MAX_LINK_LIFETIME_S: Final = 60 * 60
"""Migration 0008's CHECK: a login link lives at most an hour (P1's 15 minutes is a setting)."""
UI_LANGUAGE_PATTERN: Final = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$"
"""A language tag, as `evalset.corpus.LanguageTag` (the widget configuration's list of languages
is checked in code, PR 1's widget step)."""
UI_LANGUAGE_MAX: Final = 35
"""RFC 5646 §4.4.1's smallest buffer a tag must fit [proposal]."""
DISPLAY_NAME_MAX: Final = 80
CUSTOMER_REF_PREFIX: Final = "s-"
"""S4: a session's `customer_ref` is `s-` plus its uuid's 32 hex digits (34 characters)."""

UiLanguage = Annotated[
    str, Field(min_length=2, max_length=UI_LANGUAGE_MAX, pattern=UI_LANGUAGE_PATTERN)
]


class SessionKind(StrEnum):
    """Migration 0008's `sessions.kind`, in its order."""

    CUSTOMER = "customer"
    STAFF = "staff"


class StaffRole(StrEnum):
    """Migration 0008's `staff_users.role`, in its order (P2): `reviewer` works the queue and the
    extraction review; `admin` may do all of that and the staff upload."""

    REVIEWER = "reviewer"
    ADMIN = "admin"


ROLE_GRANTS: Final[dict[StaffRole, frozenset[StaffRole]]] = {
    StaffRole.REVIEWER: frozenset({StaffRole.REVIEWER}),
    StaffRole.ADMIN: frozenset({StaffRole.REVIEWER, StaffRole.ADMIN}),
}
"""Which route roles a staff member's role may use (P2: an admin is also a reviewer)."""


class Channel(StrEnum):
    """Migration 0008's `sessions.channel`: the web widget and dashboard only in M8 (Q5)."""

    WEB = "web"


@dataclass(frozen=True)
class CustomerSession:
    """A customer's session (S1, S4): the business and the customer come from here only."""

    business_id: BusinessId
    session_id: UUID
    customer_ref: CustomerRef
    ui_language: str


@dataclass(frozen=True)
class StaffSession:
    """A staff member's session (S5): the business, the member and the role come from here only,
    read on every request."""

    business_id: BusinessId
    session_id: UUID
    staff_id: UUID
    role: StaffRole
    display_name: str

    def may(self, required: StaffRole) -> bool:
        """Whether this member's role may use a route that requires `required`."""
        return required in ROLE_GRANTS[self.role]


@dataclass(frozen=True)
class UsedLink:
    """A login link just used (S5): the business of its row, the staff member it logs in, and
    whether that member was enabled when it was used (a disabled member's link is used up all the
    same, and the login opens no session)."""

    business_id: BusinessId
    staff_id: UUID
    enabled: bool


@dataclass(frozen=True)
class NewSession:
    """A session just stored: its id and the database's expiry. The token stays with the caller,
    which made it (`tokens.make_token`) and hands it to the client once."""

    session_id: UUID
    expires_at: datetime


def customer_ref_for(session_id: UUID) -> CustomerRef:
    """S4: `s-` plus the session uuid's 32 lowercase hex digits. An identifier, not a credential:
    it fits `tools.config.CUSTOMER_REF_PATTERN` and migration 0006's CHECKs unchanged."""
    return f"{CUSTOMER_REF_PREFIX}{session_id.hex}"


def check_lifetime(lifetime_s: int, maximum_s: int) -> int:
    """`lifetime_s` if it is more than zero and at most `maximum_s`, else `ValueError`."""
    if not 0 < lifetime_s <= maximum_s:
        raise ValueError(f"a lifetime is more than 0 and at most {maximum_s} seconds")
    return lifetime_s


CUSTOMER_SESSION: Final = text(
    "SELECT business_id, id, ui_language, token_sha256 FROM sessions"
    " WHERE business_id = :business_id AND token_sha256 = :token_sha256"
    " AND kind = 'customer' AND expires_at > now() AND revoked_at IS NULL"
)
"""A live customer session by its token (S1, S3)."""

STAFF_SESSION: Final = text(
    "SELECT s.business_id, s.id, s.staff_id, s.token_sha256, u.role, u.display_name"
    " FROM sessions s JOIN staff_users u ON u.business_id = s.business_id AND u.id = s.staff_id"
    " WHERE s.business_id = :business_id AND s.token_sha256 = :token_sha256"
    " AND s.kind = 'staff' AND s.expires_at > now() AND s.revoked_at IS NULL"
    " AND u.disabled_at IS NULL"
)
"""A live staff session by its token, with its enabled member's role (S5): the join pairs the
business, so a member of another business with the same id is never read."""

INSERT_SESSION: Final = text(
    "INSERT INTO sessions (business_id, kind, staff_id, token_sha256, channel, ui_language,"
    " expires_at)"
    " VALUES (:business_id, :kind, :staff_id, :token_sha256, :channel, :ui_language,"
    " now() + make_interval(secs => CAST(:lifetime_s AS double precision)))"
    " RETURNING id, expires_at"
)
"""`expires_at` from the database's clock; the id and `created_at` from its defaults (the app role
cannot choose either: migration 0008's column grants)."""

REVOKE_SESSION: Final = text(
    "UPDATE sessions SET revoked_at = now()"
    " WHERE business_id = :business_id AND id = :session_id"
    " AND revoked_at IS NULL AND expires_at > now()"
    " RETURNING id"
)
"""Logout: revoke one live session (`revoked_at` is the app role's only UPDATE column)."""

USE_LOGIN_LINK: Final = text(
    "UPDATE staff_login_links SET used_at = now()"
    " WHERE business_id = :business_id AND token_sha256 = :token_sha256"
    " AND used_at IS NULL AND expires_at > now()"
    " RETURNING business_id, staff_id, token_sha256,"
    " (SELECT u.disabled_at IS NULL FROM staff_users u"
    " WHERE u.business_id = staff_login_links.business_id"
    " AND u.id = staff_login_links.staff_id) AS enabled"
)
"""S5's atomic use of a one-time link: at most one caller ever gets its staff member, with whether
that member is enabled (module docstring: the subquery pairs the business)."""


async def find_customer_session(
    connection: AsyncConnection, token: ParsedToken
) -> CustomerSession | None:
    """The live customer session `token` opens, or None. More than one row raises."""
    row = (
        await connection.execute(
            CUSTOMER_SESSION,
            {"business_id": token.business_id, "token_sha256": token.sha256},
        )
    ).one_or_none()
    if row is None or not same_hash(token.sha256, row.token_sha256):
        return None
    return CustomerSession(
        business_id=row.business_id,
        session_id=row.id,
        customer_ref=customer_ref_for(row.id),
        ui_language=row.ui_language,
    )


async def find_staff_session(
    connection: AsyncConnection, token: ParsedToken
) -> StaffSession | None:
    """The live staff session `token` opens, of an enabled member, or None. More than one row
    raises."""
    row = (
        await connection.execute(
            STAFF_SESSION,
            {"business_id": token.business_id, "token_sha256": token.sha256},
        )
    ).one_or_none()
    if row is None or not same_hash(token.sha256, row.token_sha256):
        return None
    return StaffSession(
        business_id=row.business_id,
        session_id=row.id,
        staff_id=row.staff_id,
        role=StaffRole(row.role),
        display_name=row.display_name,
    )


async def insert_session(
    connection: AsyncConnection,
    token: ParsedToken,
    *,
    kind: SessionKind,
    lifetime_s: int,
    staff_id: UUID | None = None,
    ui_language: str | None = None,
    channel: Channel = Channel.WEB,
) -> NewSession:
    """Store a session for `token` (made by `tokens.make_token` for the same business): a
    customer session with its `ui_language` and no staff member, or a staff session with its
    `staff_id` and no language (migration 0008's CHECKs). `ValueError` before any SQL when the
    shape or the lifetime is wrong."""
    check_lifetime(lifetime_s, MAX_SESSION_LIFETIME_S)
    if (kind is SessionKind.STAFF) != (staff_id is not None):
        raise ValueError("a staff session has a staff member and a customer session has none")
    if (kind is SessionKind.CUSTOMER) != (ui_language is not None):
        raise ValueError("a customer session has a UI language and a staff session has none")
    row = (
        await connection.execute(
            INSERT_SESSION,
            {
                "business_id": token.business_id,
                "kind": kind.value,
                "staff_id": staff_id,
                "token_sha256": token.sha256,
                "channel": channel.value,
                "ui_language": ui_language,
                "lifetime_s": lifetime_s,
            },
        )
    ).one()
    return NewSession(session_id=row.id, expires_at=row.expires_at)


async def revoke_session(
    connection: AsyncConnection, business_id: BusinessId, session_id: UUID
) -> bool:
    """Revoke one live session of `business_id`; whether one was revoked now (False: unknown,
    expired or already revoked, which a logout treats alike)."""
    row = (
        await connection.execute(
            REVOKE_SESSION, {"business_id": business_id, "session_id": session_id}
        )
    ).one_or_none()
    return row is not None


async def use_login_link(connection: AsyncConnection, token: ParsedToken) -> UsedLink | None:
    """S5: mark `token`'s link used, once; its business, the staff member it logs in and whether
    that member is enabled now, or None (unknown, used or expired, which a login treats alike). A
    disabled member's link is used up too: the caller opens no session for it (and the session
    lookup refuses a disabled member on every request anyway, `find_staff_session`). More than one
    row raises."""
    row = (
        await connection.execute(
            USE_LOGIN_LINK,
            {"business_id": token.business_id, "token_sha256": token.sha256},
        )
    ).one_or_none()
    if row is None or not same_hash(token.sha256, row.token_sha256):
        return None
    # `is True`: a member the subquery did not find (NULL) is never taken for an enabled one.
    return UsedLink(business_id=row.business_id, staff_id=row.staff_id, enabled=row.enabled is True)
