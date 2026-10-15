"""The response-cache hook (ADR 0006, Caching). Off by default: no cache passed, no caching.

A hit returns an earlier answer to the very same request without calling a model, at $0. Two
businesses never share an entry: the key starts with the `business_id` and hashes it, and the stored
value carries it too, checked on every read (a mismatch is a miss).

How the gateway uses a cache (`LLMGateway`):

- **Fails open.** An error from `get` is a miss: the model is called and nothing is written for that
  call. An error from `set` is swallowed. Neither is raised to the caller or logged.
- **Untrusted on read** (CLAUDE.md, rule 5). A stored value is validated as `CachedResponse`; one
  that does not validate, or (structured calls) whose text no longer validates against the schema,
  is a miss, and the fresh answer then overwrites it.
- **Only successes are written:** an `end_turn` answer (for a structured call, one that validated).
  Refusals, truncations, validation failures and errors never are.

There is no in-package implementation (an in-memory cache was rejected in M1 planning); the test
double lives in `tests/`. M9's Redis store must give entries a TTL, purge one business by its key
prefix, and bound its own I/O with short timeouts: the gateway treats an error as a miss, but a
`get` that hangs uses up the call's deadline (a `set` is cut off at the deadline and dropped, and
until then holds the answer). It must never raise `CancelledError` unless the call was cancelled:
that always propagates (a cancel is never a miss). Cache errors leave no trace in M1.
"""

import hashlib
import json
from typing import Literal, Protocol

from vectapilot_api.gateway.providers.base import ProviderRequest
from vectapilot_api.gateway.types import BusinessId, ContentBlock, GatewayModel, OpaqueId


class ResponseCache(Protocol):
    """Where cached answers live. Values are `CachedResponse` JSON text; the gateway (de)serialises
    and validates them, so a store only moves strings."""

    async def get(self, key: str) -> str | None:
        """The value stored under `key`, or None."""

    async def set(self, key: str, value: str) -> None:
        """Store `value` under `key`, replacing any earlier value."""


class CachedResponse(GatewayModel):
    """What one cache entry holds: the answer's content and who it belongs to. No usage: a hit is
    billed nothing."""

    business_id: BusinessId
    """Checked on every read: an entry of another business is never served."""
    model_answered: OpaqueId
    stop_reason: Literal["end_turn"]
    """The only stop reason ever cached (refusals and truncations are typed errors)."""
    content: tuple[ContentBlock, ...]
    """As the model returned it, thinking blocks included (they round-trip unchanged)."""


def cache_key(
    *,
    business_id: BusinessId,
    catalog_version: str,
    request: ProviderRequest,
    schema: str | None,
) -> str:
    """`<business_id>:<sha256 hex>`, over canonical JSON of everything that shapes the answer.

    Hashed: `business_id`, the catalog version, and the provider request as sent (model id,
    effort and `max_tokens` (the output params), system prompt, messages), plus `schema`: the
    structured call's schema identity, None for an unstructured call. Not hashed: `trace_id` and
    `operation`, which do not change the answer. Canonical JSON = sorted keys, no spaces, ASCII.
    """
    payload = {
        "business_id": business_id,
        "catalog_version": catalog_version,
        "request": request.model_dump(mode="json"),
        "schema": schema,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{business_id}:{hashlib.sha256(canonical.encode('ascii')).hexdigest()}"
