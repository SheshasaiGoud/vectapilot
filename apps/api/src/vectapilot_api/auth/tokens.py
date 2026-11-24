"""Session and login-link tokens (S3; D1 (a), D4 (a)): `<business_id>.<secret>`.

- **256 random bits.** The secret is `secrets.token_urlsafe(32)`: 32 bytes, 43 URL-safe
  characters (base64url without padding, ceil(32 x 4 / 3), arithmetic). Guessing one is out of
  reach, so the token alone is the credential.
- **The business is the prefix.** A lookup reads the business from the token, opens that
  business's RLS transaction (`db.business_transaction`) and reads `(business_id, token_sha256)`
  there. A prefix edited to name another business is simply not found in that business: no
  `SECURITY DEFINER` function and no `BYPASSRLS` role are needed (ADR 0019 decision 1). The prefix
  is not a secret: it names the business the token belongs to, as the widget's URL does.
- **Only the SHA-256 of the whole token is stored** (`sessions.token_sha256`,
  `staff_login_links.token_sha256`, migration 0008), as for confirm tokens (`actions.tokens`): a
  plain hash, not a password hash, because the secret is random and long. The hash covers the
  prefix too, so an edited prefix also changes the hash. Someone who reads the table cannot log in.
- **Never logged, never in a URL query** (S3, S18): a token lives in a request header (the
  widget's `Authorization: Bearer`), the staff cookie, or a login link's URL fragment, which no
  server log or `Referer` ever receives. `ParsedToken`'s repr leaves the hash out.

`parse_token` raises `InvalidToken` for anything that is not exactly this form, with a fixed
message: the value is caller input and a credential.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from typing import Final

from vectapilot_api.db import InvalidBusinessId, parse_business_id
from vectapilot_api.gateway.types import BusinessId

SECRET_BYTES: Final = 32
"""256 bits (S3)."""
SECRET_LENGTH: Final = 43
"""`token_urlsafe(32)`'s length: base64url without padding, ceil(32 x 4 / 3)."""
SEPARATOR: Final = "."
"""Between the business and the secret. Neither a business id (a lowercase slug) nor base64url
ever holds one, so the split is unambiguous."""
BUSINESS_ID_MAX: Final = 40
"""`gateway.types.BusinessId`'s longest form."""
TOKEN_MAX: Final = BUSINESS_ID_MAX + len(SEPARATOR) + SECRET_LENGTH
"""84 characters (arithmetic): anything longer is refused before any other work."""
SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
"""The stored form (migration 0008's CHECK)."""

_SECRET: Final = re.compile(rf"[A-Za-z0-9_-]{{{SECRET_LENGTH}}}")


class InvalidToken(ValueError):
    """Not a token of this form. The message never contains the value: it is a credential."""


@dataclass(frozen=True)
class ParsedToken:
    """A token's business and the SHA-256 of the whole token: what a lookup needs. The token
    itself is not kept, and the hash is left out of the repr (S3: never in a log line)."""

    business_id: BusinessId
    sha256: str = field(repr=False)


def token_sha256(token: str) -> str:
    """The stored form: the SHA-256 of the token's UTF-8 bytes, 64 lowercase hex digits."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_token(business_id: str) -> str:
    """A fresh token for `business_id` (256 random bits). `InvalidBusinessId` for a bad id."""
    valid = parse_business_id(business_id)
    return f"{valid}{SEPARATOR}{secrets.token_urlsafe(SECRET_BYTES)}"


def parse_token(token: str) -> ParsedToken:
    """`token`'s business and hash, or `InvalidToken`: too long, no separator, a business that is
    not a `BusinessId`, or a secret that is not exactly 43 base64url characters."""
    if len(token) > TOKEN_MAX:
        raise InvalidToken("not a valid token")
    business, separator, secret = token.rpartition(SEPARATOR)
    if not separator or _SECRET.fullmatch(secret) is None:
        raise InvalidToken("not a valid token")
    try:
        business_id = parse_business_id(business)
    except InvalidBusinessId:
        raise InvalidToken("not a valid token") from None
    return ParsedToken(business_id=business_id, sha256=token_sha256(token))


def same_hash(expected_sha256: str, stored_sha256: str) -> bool:
    """Whether two hex digests are equal, compared in constant time (as `actions.tokens`): for a
    caller that compares a hash in Python rather than in the lookup's SQL."""
    return hmac.compare_digest(expected_sha256, stored_sha256)
