"""M8 PR 1's session bait for the canary suite (migration 0008; S3, S5, ADR 0019 decision 8).

Every planted business gets rows in the three new tables, so the catalog-wide checks (every
business table shows rows, and only its own) cover them from the day they exist, and the auth
read paths (`read_paths.py`) and the session attack tests (`test_sessions.py`) have something to
leak.

**Shared on purpose, as the sandbox bait** (`read_paths.BAIT_SLOT_IDS`): both businesses hold
the same session id, staff member id and staff session id, and the same *hashes* under them (a
customer session's, a staff session's and a login link's). A hash can never be shared by real
tokens (the hash covers the business prefix), so this is a planted collision: with it, a lookup
or the link use that lost its own `business_id` clause reads two rows on the owner's connection
(no RLS) and raises instead of choosing. The staff member's display name is the business's
canary, so a staff lookup that read across would show the other canary.

**Real tokens per business** (`bait_token`): derived from the business id like
`read_paths.bait_action_id` (a planted business id is random per run, so these are too), each in
the exact token form (`auth.tokens`), for the tests that go through the dependencies: a live
customer session, a live staff session, an expired one, a revoked one, a disabled member's
session and a fresh login link. All of them synthetic.

**Lifetimes are long on purpose**: a live row stays live for the whole test (an hour or more), so
a "nothing changed" comparison never flips because a row expired in between. The expired session
is expired from the start (created 13 hours ago with a 12-hour lifetime).

The owner plants what only the owner may write (staff members, links, rows with a chosen id or a
past `created_at`); the app role plants the rest through `auth.store`, which also proves its
column grants under RLS (`insert_session`, `revoke_session`).
"""

import base64
import hashlib
from enum import StrEnum
from typing import Any, Final
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.auth.store import SessionKind, insert_session, revoke_session
from vectapilot_api.auth.tokens import parse_token, token_sha256

BAIT_SESSION_ID: Final = uuid5(NAMESPACE_URL, "vectapilot-canary-session")
BAIT_STAFF_SESSION_ID: Final = uuid5(NAMESPACE_URL, "vectapilot-canary-staff-session")
BAIT_STAFF_ID: Final = uuid5(NAMESPACE_URL, "vectapilot-canary-staff")
BAIT_DISABLED_STAFF_ID: Final = uuid5(NAMESPACE_URL, "vectapilot-canary-disabled-staff")
SHARED_CUSTOMER_SHA256: Final = token_sha256("vectapilot-canary-shared-customer-session")
SHARED_STAFF_SHA256: Final = token_sha256("vectapilot-canary-shared-staff-session")
SHARED_LINK_SHA256: Final = token_sha256("vectapilot-canary-shared-login-link")
LIVE_HOURS: Final = 2
"""The app-planted sessions' lifetime: far beyond any test, inside the 24-hour CHECK."""


class Purpose(StrEnum):
    """What a per-business bait token opens (`bait_token`)."""

    CUSTOMER = "customer"
    STAFF = "staff"
    EXPIRED = "expired"
    REVOKED = "revoked"
    DISABLED_STAFF = "disabled-staff"
    LINK = "link"


def bait_token(business_id: str, purpose: Purpose) -> str:
    """`business_id`'s bait token for `purpose`: the exact token form, `<business_id>.<43
    base64url characters>`, derived (not random) so a test can name it without the database."""
    digest = hashlib.sha256(f"vectapilot-canary-token:{purpose}:{business_id}".encode()).digest()
    secret = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{business_id}.{secret}"


def bait_sha256(business_id: str, purpose: Purpose) -> str:
    return token_sha256(bait_token(business_id, purpose))


OWNER_SESSION_PLANT: Final = (
    text(
        "INSERT INTO staff_users (business_id, id, display_name, role)"
        " VALUES (:b, :staff_id, :canary, 'reviewer')"
    ),
    text(
        "INSERT INTO staff_users (business_id, id, display_name, role, disabled_at)"
        " VALUES (:b, :disabled_staff_id, :canary, 'admin', now())"
    ),
    text(
        "INSERT INTO staff_login_links (business_id, staff_id, token_sha256, expires_at)"
        " VALUES (:b, :staff_id, :shared_link_sha256, now() + interval '1 hour')"
    ),
    text(
        "INSERT INTO staff_login_links (business_id, staff_id, token_sha256, expires_at)"
        " VALUES (:b, :staff_id, :link_sha256, now() + interval '1 hour')"
    ),
    text(
        "INSERT INTO sessions (business_id, id, kind, token_sha256, channel, ui_language,"
        " expires_at) VALUES (:b, :session_id, 'customer', :shared_customer_sha256, 'web', 'en',"
        " now() + interval '12 hours')"
    ),
    text(
        "INSERT INTO sessions (business_id, id, kind, staff_id, token_sha256, channel,"
        " expires_at) VALUES (:b, :staff_session_id, 'staff', :staff_id, :shared_staff_sha256,"
        " 'web', now() + interval '8 hours')"
    ),
    text(
        "INSERT INTO sessions (business_id, kind, token_sha256, channel, ui_language, created_at,"
        " expires_at) VALUES (:b, 'customer', :expired_sha256, 'web', 'en',"
        " now() - interval '13 hours', now() - interval '1 hour')"
    ),
)
"""What only the owner may plant (staff members, links, chosen ids, a past `created_at`)."""

SESSION_UPROOT: Final = (
    text("DELETE FROM sessions WHERE business_id = :b"),
    text("DELETE FROM staff_login_links WHERE business_id = :b"),
    text("DELETE FROM staff_users WHERE business_id = :b"),
)
"""Children first (both reference `staff_users`), before the business itself."""


def owner_session_params(business_id: str) -> dict[str, Any]:
    """`OWNER_SESSION_PLANT`'s parameters for `business_id`, but `b` and `canary`."""
    return {
        "staff_id": BAIT_STAFF_ID,
        "disabled_staff_id": BAIT_DISABLED_STAFF_ID,
        "session_id": BAIT_SESSION_ID,
        "staff_session_id": BAIT_STAFF_SESSION_ID,
        "shared_customer_sha256": SHARED_CUSTOMER_SHA256,
        "shared_staff_sha256": SHARED_STAFF_SHA256,
        "shared_link_sha256": SHARED_LINK_SHA256,
        "link_sha256": bait_sha256(business_id, Purpose.LINK),
        "expired_sha256": bait_sha256(business_id, Purpose.EXPIRED),
    }


async def plant_app_sessions(connection: AsyncConnection, business_id: str) -> None:
    """What the app role plants itself, through `auth.store` (inside `business_id`'s
    transaction): a live customer session, a live staff session, a revoked customer session and a
    disabled member's staff session, each under its bait token."""
    hours = LIVE_HOURS * 60 * 60
    for purpose, language in ((Purpose.CUSTOMER, "en"), (Purpose.REVOKED, "hi")):
        await insert_session(
            connection,
            parse_token(bait_token(business_id, purpose)),
            kind=SessionKind.CUSTOMER,
            lifetime_s=hours,
            ui_language=language,
        )
    for purpose, staff_id in (
        (Purpose.STAFF, BAIT_STAFF_ID),
        (Purpose.DISABLED_STAFF, BAIT_DISABLED_STAFF_ID),
    ):
        await insert_session(
            connection,
            parse_token(bait_token(business_id, purpose)),
            kind=SessionKind.STAFF,
            lifetime_s=hours,
            staff_id=staff_id,
        )
    revoked = await connection.scalar(
        text("SELECT id FROM sessions WHERE business_id = :b AND token_sha256 = :sha256"),
        {"b": business_id, "sha256": bait_sha256(business_id, Purpose.REVOKED)},
    )
    assert await revoke_session(connection, business_id, revoked), "the bait was not revoked"
