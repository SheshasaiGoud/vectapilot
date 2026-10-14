"""What the gateway sends to a provider, and what it gets back, for ONE attempt.

The fake provider (`fake.py`) and the Anthropic adapter (PR 5) both implement `Provider`. The
adapter validates every provider value into these types at its boundary, so the gateway never sees
an SDK type. Retries, time limits, repair and cost are the gateway's job (PR 6), not a provider's:
one `send` is one attempt.
"""

from typing import Annotated, Protocol

from pydantic import Field

from vectapilot_api.gateway.types import (
    ContentBlock,
    Effort,
    GatewayModel,
    Message,
    OpaqueId,
    RequestId,
    StopReason,
    Usage,
)


class ProviderRequest(GatewayModel):
    """One attempt, as the gateway resolved it: the catalog already turned the alias into these."""

    model: OpaqueId
    """The model id from the catalog (never an alias)."""
    effort: Effort
    max_tokens: int = Field(gt=0, strict=True)
    """The effective output cap: the request's `max_tokens`, else the alias's maximum."""
    system: Annotated[str, Field(min_length=1)] | None = Field(default=None, repr=False)
    messages: tuple[Message, ...] = Field(min_length=1)


class ProviderResponse(GatewayModel):
    """One attempt's answer, whatever its stop reason (a refusal or truncation included).

    The gateway decides what a stop reason means (result or typed error); this type only carries
    it. `inference_geo` is the provider's `usage.inference_geo`, kept next to `usage` rather than
    in it (see `types.Usage`).
    """

    content: tuple[ContentBlock, ...]
    stop_reason: StopReason
    usage: Usage
    model: OpaqueId
    """The model id that answered; it can differ from the requested one, and cost uses this."""
    request_id: RequestId | None = None
    inference_geo: OpaqueId | None = None
    """`global` or `us` as reported; None if the provider did not say. Anything but `global`, None
    included, makes the call's cost incomplete (fail closed, ADR 0006 Cost)."""


class Provider(Protocol):
    """A model provider: one `send` per attempt; failures raise a typed `GatewayError`."""

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        """Make one attempt. Never retries, never logs request or response text."""
