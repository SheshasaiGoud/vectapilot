"""The one-time confirm token (decided D3 (a), W2).

- **128 bits** from `secrets.token_urlsafe(16)`: 22 URL-safe characters. Guessing one is out of
  reach, so the token, not the action id (which appears in URLs, logs and the audit), is the
  credential.
- **Only its SHA-256 is stored** (`pending_actions.confirm_token_sha256`). A plain hash, not a
  password hash: the token is random and long, so there is nothing to brute-force, and a slow hash
  would only slow every confirm down. Someone who reads the table cannot confirm.
- **Compared in constant time** (`hmac.compare_digest` over the two hex digests), so the response
  time does not tell how many leading characters of a guess were right.
- **Never logged, audited or shown to a model** (W2): it exists in memory between proposal and
  response, and in the confirm request's body. `ConfirmToken` is a pydantic `Secret`, so a repr, a
  log extra, `model_dump` and even `model_dump_json` print `**********`. PR 4's `/v1/agent`
  response is the one place that reveals it (`get_secret_value()`, on purpose, in its serializer).
"""

import hashlib
import hmac
import secrets
from typing import Annotated, Final

from pydantic import Secret, StringConstraints

TOKEN_BYTES: Final = 16
"""128 bits (W2)."""
TOKEN_LENGTH: Final = 22
"""`token_urlsafe(16)`'s length: base64url without padding, ceil(16 * 4 / 3)."""
TOKEN_PATTERN: Final = rf"^[A-Za-z0-9_-]{{{TOKEN_LENGTH}}}$"

TokenText = Annotated[
    str, StringConstraints(min_length=TOKEN_LENGTH, max_length=TOKEN_LENGTH, pattern=TOKEN_PATTERN)
]
ConfirmToken = Secret[TokenText]
"""A confirm token in a request or a result: validated as `TokenText`, masked everywhere else."""


def new_confirm_token() -> ConfirmToken:
    """A fresh one-time token (128 bits)."""
    return ConfirmToken(secrets.token_urlsafe(TOKEN_BYTES))


def token_sha256(token: ConfirmToken) -> str:
    """The stored form: the SHA-256 of the token's UTF-8 bytes, 64 lowercase hex digits."""
    return hashlib.sha256(token.get_secret_value().encode("utf-8")).hexdigest()


def token_matches(token: ConfirmToken, stored_sha256: str) -> bool:
    """Whether `token` is the one whose hash is stored, compared in constant time."""
    return hmac.compare_digest(token_sha256(token), stored_sha256)
